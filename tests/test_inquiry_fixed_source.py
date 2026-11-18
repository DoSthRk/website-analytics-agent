from dataclasses import replace
from datetime import date,datetime,timezone
from pathlib import Path

import pytest

from test_inquiry import _Cursor,_Connection
from website_analytics.models import InquirySourceConfig,DateRange
from website_analytics.config import load_sites,ConfigError
from website_analytics.adapters.inquiry import LegacyContactsAdapter,create_inquiry_adapter,InquiryScopeError,inquiry_source_data
from website_analytics.analysis.readers import AdapterReader
from website_analytics.analysis.contracts import Selection
from test_analysis_readers import site

SOURCE=InquirySourceConfig('legacy_contacts_mysql','WEBSITE_ANALYTICS_TEST_DSN',site_scope='fixed_registered_source',site_scope_evidence='Synthetic fixed binding and origin observation, not exclusive writers.',registered_database='v2_genemedi_net',registered_table='contacts',allowed_hosts=('genemedi.net','www.genemedi.net'))
PERIOD=DateRange(date(2026,10,19),date(2026,10,25))


def adapter(url='https://www.genemedi.net/i/Mixed',database='v2_genemedi_net',total=2):
    cursor=_Cursor([[{'selected_database':database}],
        [{'date':PERIOD.start,'stored_submissions':total,'quarantined_submissions':0,'non_quarantined_submissions':total}],
        [{'source_url':url,'stored_submissions':1,'quarantined_submissions':0,'non_quarantined_submissions':1}]])
    connection=_Connection(cursor)
    return LegacyContactsAdapter('mysql://synthetic:synthetic@db.example.test/v2_genemedi_net',connector=lambda **_:connection,domains=SOURCE.allowed_hosts,registered_database=SOURCE.registered_database),connection,cursor


def config(tmp_path,**patch):
    import yaml
    raw={'kind':SOURCE.kind,'credential_env':SOURCE.credential_env,'site_scope':SOURCE.site_scope,'site_scope_evidence':SOURCE.site_scope_evidence,'registered_database':SOURCE.registered_database,'registered_table':SOURCE.registered_table,'allowed_hosts':list(SOURCE.allowed_hosts)};raw.update(patch)
    path=tmp_path/'sites.yaml';path.write_text(yaml.safe_dump({'sites':{'genemedi-net':{'display_name':'Synthetic','domains':list(SOURCE.allowed_hosts),'timezone':'America/Los_Angeles','ga4_property_id':'123','gsc_property_url':'https://www.genemedi.net/','inquiry_source':raw}}}))
    return path


def test_fixed_binding_and_unknown_counts_preserve_no_dedication_or_timezone_claim(tmp_path):
    loaded=load_sites(config(tmp_path))['genemedi-net'].inquiry_source
    assert loaded==SOURCE
    a,c,cur=adapter();result=a.query(PERIOD)
    assert result.site_scope=='fixed_registered_source' and result.unknown_attribution_count==1
    assert result.totals['storedSubmissions']==2 and result.pages[0]['sourceUrl'].endswith('/i/Mixed')
    assert cur.executed[0][0]=='SELECT DATABASE() AS selected_database' and c.closed
    a,_,_=adapter();reader=AdapterReader(site(),inquiry=a,exact_paths=True,clock=lambda:datetime(2026,11,3,tzinfo=timezone.utc))
    evidence=reader.read('inquiry',PERIOD,('/i/Mixed',),Selection())
    assert evidence.status=='partial' and evidence.metrics['storedSubmissions']==1
    assert evidence.unattributed_submissions==1 and evidence.date_boundary=='unverified'
    a,_,_=adapter();_,totals,metadata=inquiry_source_data(a,PERIOD)
    assert metadata['site_scope']=='fixed_registered_source_not_physical_dedication'
    assert metadata['status']=='partial' and totals['storedSubmissions']==2


@pytest.mark.parametrize('patch',[{'registered_database':'bad/schema'},{'registered_table':'users'},{'allowed_hosts':['tarmart.com']},{'allowed_hosts':[]},{'allowed_hosts':['genemedi.net','GENEMEDI.NET']},{'site_scope_evidence':''}])
def test_invalid_bindings_fail_offline(tmp_path,patch):
    with pytest.raises(ConfigError):load_sites(config(tmp_path,**patch))


@pytest.mark.parametrize('patch',[{'registered_database':'wrong'},{'registered_table':'users'},{'allowed_hosts':('tarmart.com',)}])
def test_factory_scope_and_dsn_drift_fail_closed(monkeypatch,patch):
    monkeypatch.setenv(SOURCE.credential_env,'mysql://synthetic:synthetic@db.example.test/v2_genemedi_net')
    with pytest.raises(InquiryScopeError):create_inquiry_adapter(replace(SOURCE,**patch),domains=SOURCE.allowed_hosts)


def test_database_drift_fails_before_aggregate_queries():
    a,c,cur=adapter(database='wrong')
    with pytest.raises(InquiryScopeError):a.query(PERIOD)
    assert c.closed and len(cur.executed)==1


@pytest.mark.parametrize('url',['https://tarmart.com/i/Mixed','//tarmart.com/i/Mixed','https://genemedi.net.evil.test/i/Mixed'])
def test_foreign_origins_fail_entire_source(url):
    a,c,_=adapter(url)
    with pytest.raises(InquiryScopeError):a.query(PERIOD)
    assert c.closed


@pytest.mark.parametrize('url',['/i/Mixed','[UNATTRIBUTED]','https://synthetic:synthetic@www.genemedi.net/i/Mixed'])
def test_unknown_attribution_is_not_silently_assigned(url):
    a,_,_=adapter(url);result=a.query(PERIOD)
    assert result.unknown_attribution_count==2 and result.pages[0]['sourceUrl']=='[UNATTRIBUTED]'
