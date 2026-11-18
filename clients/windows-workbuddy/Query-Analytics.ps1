# Windows PowerShell 5.1 / PowerShell 7. Read-only, fixed loopback destination.
[CmdletBinding()]
param(
 [ValidateSet('health','list_cases','case_versions','case_observations','case_detail','case_trend','page_contribution')]
 [string]$Operation = 'list_cases',
 [string]$CaseId,
 [string]$EvidenceId,
 [string]$PageKey
)
$ErrorActionPreference = 'Stop'
$client = $null
$handler = $null
$notice = @{ saved_data_only=$true; automatic_refresh_not_enabled=$true; windows_device_acceptance='not_verified'; data_status_is_not_causal_evidence=$true }
try {
 $needCase = $Operation -notin @('health','list_cases')
 $needEvidence = $Operation -in @('case_detail','case_trend','page_contribution')
 $needPage = $Operation -eq 'page_contribution'
 if (($needCase -and $CaseId -cnotmatch '^[a-z][a-z0-9-]{0,63}$') -or (-not $needCase -and $CaseId)) { throw 'invalid selector' }
 if (($needEvidence -and $EvidenceId -cnotmatch '^[a-f0-9]{64}$') -or (-not $needEvidence -and $EvidenceId)) { throw 'invalid selector' }
 if (($needPage -and $PageKey -cnotmatch '^[a-f0-9]{64}$') -or (-not $needPage -and $PageKey)) { throw 'invalid selector' }
 Add-Type -AssemblyName System.Net.Http
 $handler = New-Object System.Net.Http.HttpClientHandler
 $handler.UseProxy = $false
 $handler.AllowAutoRedirect = $false
 $client = New-Object System.Net.Http.HttpClient($handler)
 $client.Timeout = [TimeSpan]::FromSeconds(10)
 $client.MaxResponseContentBufferSize = 1000000
 if ($Operation -eq 'health') {
  $response = $client.GetAsync('http://127.0.0.1:8765/healthz').GetAwaiter().GetResult()
 } else {
  $body = @{ contract='batch-service/v1'; operation=$Operation }
  if ($needCase) { $body.case_id=$CaseId }
  if ($needEvidence) { $body.evidence_id=$EvidenceId }
  if ($needPage) { $body.page_key=$PageKey }
  $json = ConvertTo-Json -InputObject $body -Compress -Depth 10
  $content = New-Object System.Net.Http.StringContent($json,[Text.Encoding]::UTF8,'application/json')
  $response = $client.PostAsync('http://127.0.0.1:8765/v1/query',$content).GetAwaiter().GetResult()
 }
 $response.EnsureSuccessStatusCode() | Out-Null
 $result = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
 if ($result.contract -ne 'batch-service/v1' -or $result.status -notin @('ok','partial')) { throw 'invalid response' }
 if ($Operation -eq 'health') {
  if ($result.binding -ne 'loopback_only' -or $result.live_sources -ne $false) { throw 'unexpected health' }
 } else {
  if ($result.operation -ne $Operation) { throw 'response identity mismatch' }
  if ($needCase -and $result.case_id -ne $CaseId) { throw 'response identity mismatch' }
  if ($needEvidence -and $result.evidence_id -cne $EvidenceId) { throw 'response identity mismatch' }
  if ($needPage -and $result.page_key -cne $PageKey) { throw 'response identity mismatch' }
 }
 @{ client_contract='genemedi-readonly-client/v1'; notice=$notice; server_result=$result } | ConvertTo-Json -Depth 100
} catch {
 @{ client_contract='genemedi-readonly-client/v1'; status='error'; code='local_query_failed_check_approved_tunnel'; notice=$notice } | ConvertTo-Json -Depth 5
 exit 2
} finally {
 if ($null -ne $client) { $client.Dispose() }
 if ($null -ne $handler) { $handler.Dispose() }
}
