[CmdletBinding()]
param([string]$RepoRoot = "")
$ErrorActionPreference = "SilentlyContinue"
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$runtime = Join-Path $RepoRoot "runtime"
$pidFile = Join-Path $runtime "gateway.pid"
if (Test-Path $pidFile) {
    $gatewayPid = [int](Get-Content $pidFile | Select-Object -First 1)
    if (Get-Process -Id $gatewayPid -ErrorAction SilentlyContinue) { Stop-Process -Id $gatewayPid -Force }
    Remove-Item $pidFile -Force
}
$profile = Join-Path ${env:LOCALAPPDATA} "ArenaDesktop\ChromeProfile"
Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine.Contains("--user-data-dir=$profile") } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Write-Host "Arena Desktop gateway and its dedicated Chrome profile were stopped."
