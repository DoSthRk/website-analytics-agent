[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$NodeModulesPath
)

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$targetPath = Join-Path $projectRoot "node_modules"

try {
    $sourcePath = (Get-Item -LiteralPath $NodeModulesPath -Force -ErrorAction Stop).FullName
} catch {
    throw "NodeModulesPath must be an existing directory supplied by the Codex dependency loader."
}

if (-not (Test-Path -LiteralPath $sourcePath -PathType Container)) {
    throw "NodeModulesPath must be an existing directory supplied by the Codex dependency loader."
}

$artifactToolPath = Join-Path $sourcePath "@oai\artifact-tool"
if (-not (Test-Path -LiteralPath $artifactToolPath -PathType Container)) {
    throw "NodeModulesPath does not contain @oai\artifact-tool."
}

if (Test-Path -LiteralPath $targetPath) {
    throw "Refusing to overwrite existing local node_modules at $targetPath. Remove it only after verifying it is safe to replace."
}

New-Item -ItemType Junction -Path $targetPath -Target $sourcePath -ErrorAction Stop | Out-Null

if (-not (Test-Path -LiteralPath (Join-Path $targetPath "@oai\artifact-tool") -PathType Container)) {
    throw "Artifact Tool runtime link verification failed."
}

[PSCustomObject]@{
    status = "ok"
    node_modules = $targetPath
    artifact_tool = (Join-Path $targetPath "@oai\artifact-tool")
} | ConvertTo-Json -Compress
