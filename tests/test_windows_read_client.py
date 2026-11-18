"""Portable client contract tests use synthetic evidence and loopback only."""
import importlib.util,json,shutil,subprocess,threading
from pathlib import Path
import pytest
from urllib.request import Request
from test_analysis_cases import record
from website_analytics.analysis.cases import CaseStore
from website_analytics.analysis.service import make_server

ROOT=Path(__file__).resolve().parents[1];PACKAGE=ROOT/'clients/windows-workbuddy'

def module():
    spec=importlib.util.spec_from_file_location('portable_client',PACKAGE/'analytics_query.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def test_six_read_operations_against_current_contract(tmp_path,monkeypatch):
    m=module();store=CaseStore(tmp_path/'cases.sqlite');old=record();eid=store.put(old);server=make_server(None,0,cases=store)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
    def redirected_for_fixture(url,**kw):
        assert url=='http://127.0.0.1:8765/v1/query'
        return Request('http://127.0.0.1:'+str(server.server_port)+'/v1/query',**kw)
    monkeypatch.setattr(m,'Request',redirected_for_fixture)
    try:
        assert m.query(m.payload('list_cases'))['notice']['saved_data_only']
        for op in ['case_versions','case_observations']:assert m.query(m.payload(op,old['case_id']))['server_result']['operation']==op
        detail=m.query(m.payload('case_detail',old['case_id'],eid))['server_result']
        assert m.query(m.payload('case_trend',old['case_id'],eid))['server_result']['operation']=='case_trend'
        key=detail['pages'][0]['page_key'];assert m.query(m.payload('page_contribution',old['case_id'],eid,key))['server_result']['page_key']==key
    finally:server.shutdown();server.server_close();thread.join()

@pytest.mark.parametrize('args',[('fetch',),('case_detail','../outside','0'*64),('case_detail','a','INVALID'),('list_cases','extra'),('page_contribution','a','0'*64,'../file')])
def test_client_rejects_non_read_or_invalid_selectors(args):
    with pytest.raises(ValueError):module().payload(*args)


def test_package_no_access_creation_or_unbounded_network():
    ps=(PACKAGE/'Query-Analytics.ps1').read_text()
    for required in ['UseProxy = $false','AllowAutoRedirect = $false','MaxResponseContentBufferSize = 1000000','http://127.0.0.1:8765/v1/query','ValidateSet','saved_data_only']:
        assert required in ps
    for forbidden in ['Set-ExecutionPolicy','Invoke-Expression','New-LocalUser','Add-LocalGroupMember','Invoke-WebRequest','sudo ','ssh-keygen']:
        assert forbidden not in ps
    cfg=(PACKAGE/'ssh_config.example').read_text();assert 'ADMIN_CONFIRMED_' in cfg and '127.0.0.1:8765 127.0.0.1:8765' in cfg
    assert 'root' not in cfg and 'StrictHostKeyChecking yes' in cfg
    assert not list(PACKAGE.glob('*.pem')) and not list(PACKAGE.glob('*.pub'))


def test_powershell_syntax_when_runtime_available():
    binary=shutil.which('pwsh')
    if not binary:pytest.skip('PowerShell unavailable locally; Windows device acceptance remains unverified')
    # Parse only; do not execute the client or start any SSH flow.
    script="$errors=$null;$tokens=$null;[System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors)|Out-Null;if($errors.Count){exit 2}"
    result=subprocess.run([binary,'-NoProfile','-NonInteractive','-Command',script,str(PACKAGE/'Query-Analytics.ps1')],capture_output=True,text=True)
    assert result.returncode==0


def test_direct_portable_api_rejects_extra_fields_before_network(monkeypatch):
    m=module();monkeypatch.setattr(m,'build_opener',lambda *a:pytest.fail('invalid client body reached network'))
    with pytest.raises(ValueError):m.query({'contract':'batch-service/v1','operation':'list_cases','remote_url':'https://outside.invalid'})
