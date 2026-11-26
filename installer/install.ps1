param(
    [string]$Source = 'git+https://github.com/scerial/Fluent-Monitor',
    [string]$Ref = 'main',
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'

if ([System.Environment]::OSVersion.Version.Major -lt 10) {
    Write-Host 'FluentMon needs Windows 10 or 11.'
    exit 2
}

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
}
$uvBin = Join-Path $env:USERPROFILE '.local\bin'
$env:Path = "$uvBin;$env:Path"

uv tool install --python 3.12 --force "${Source}@${Ref}"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

uv tool update-shell
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$toolBin = (uv tool dir --bin).Trim()
& (Join-Path $toolBin 'fmon.exe') setup @Rest
exit $LASTEXITCODE
