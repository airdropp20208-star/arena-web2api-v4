[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [int]$GatewayPort = 8010,
    [int]$RouterPort = 20128,
    [int]$CdpPort = 9223,
    [switch]$NoChrome
)
$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($RepoRoot)) { $RepoRoot = Split-Path -Parent $PSScriptRoot }
$RepoRoot = [IO.Path]::GetFullPath($RepoRoot)
$runtime = Join-Path $RepoRoot "runtime"
New-Item -ItemType Directory -Force $runtime | Out-Null
function Test-Http([string]$Url) {
    try { $r = Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 4; return ($r.StatusCode -ge 200 -and $r.StatusCode -lt 500) } catch { return $false }
}
function Get-Json([string]$Url) { try { return Invoke-RestMethod -Uri $Url -TimeoutSec 4 } catch { return $null } }
function Wait-Http([string]$Url, [int]$Seconds = 30) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    do { if (Test-Http $Url) { return $true }; Start-Sleep -Milliseconds 500 } while ((Get-Date) -lt $deadline)
    return $false
}
$env:HOST = "127.0.0.1"
$env:PORT = "$GatewayPort"
$env:TOKEN_BROKER_HOST = "127.0.0.1"
$env:TOKEN_BROKER_PORT = "8765"
$env:TOKEN_BROKER_ENABLED = "true"
$env:RECAPTCHA_SOLVER = "desktop_cdp"
$env:PYTHONUNBUFFERED = "1"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:DESKTOP_CDP_PORT = "$CdpPort"
$python = $env:HERMES_PYTHON
if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path $python)) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { $python = $cmd.Source }
}
if ([string]::IsNullOrWhiteSpace($python) -or -not (Test-Path $python)) { throw "Python 3 is not available. Set HERMES_PYTHON to the Hermes venv python.exe." }
$healthUrl = "http://127.0.0.1:$GatewayPort/health"
$brokerUrl = "http://127.0.0.1:$GatewayPort/admin/broker"
$pidFile = Join-Path $runtime "gateway.pid"
if (-not (Test-Http $healthUrl)) {
    $outLog = Join-Path $runtime "gateway.out.log"
    $errLog = Join-Path $runtime "gateway.err.log"
    $proc = Start-Process -FilePath $python -WorkingDirectory $RepoRoot -ArgumentList @("-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "$GatewayPort") -RedirectStandardOutput $outLog -RedirectStandardError $errLog -PassThru -WindowStyle Hidden
    Set-Content -Path $pidFile -Value $proc.Id -Encoding ASCII
    if (-not (Wait-Http $healthUrl 30)) { throw "Arena gateway did not become healthy. Check runtime/gateway.err.log." }
}
if (-not (Test-Path $pidFile)) {
    $gatewayProc = Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine.Contains($RepoRoot) -and ($_.CommandLine.Contains("main:app") -or $_.CommandLine.Contains("main.py")) } |
        Select-Object -First 1
    if ($gatewayProc) { Set-Content -Path $pidFile -Value $gatewayProc.ProcessId -Encoding ASCII }
    if (-not (Test-Path $pidFile)) {
        $listener = Get-NetTCPConnection -LocalPort $GatewayPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($listener) { Set-Content -Path $pidFile -Value $listener.OwningProcess -Encoding ASCII }
    }
}
if (Test-Http "http://127.0.0.1:$RouterPort/v1/models") { Write-Host "9Router detected on port $RouterPort." -ForegroundColor Green } else { Write-Warning "9Router was not detected on port $RouterPort; Hermes routing may be unavailable." }
if (-not $NoChrome) {
    $chromeCandidates = @()
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)}, $env:LOCALAPPDATA, "C:\Program Files", "C:\Program Files (x86)")) {
        if (-not [string]::IsNullOrWhiteSpace($base)) { $chromeCandidates += (Join-Path $base "Google\Chrome\Application\chrome.exe") }
    }
    $chrome = $chromeCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $chrome) { $chrome = Get-Process chrome -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path }
    if ($chrome) {
        $profile = Join-Path ${env:LOCALAPPDATA} "ArenaDesktop\ChromeUserData"
        $existing = Get-CimInstance Win32_Process -Filter "name='chrome.exe'" -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -and $_.CommandLine.Contains("--user-data-dir=$profile") }
        if (-not $existing) {
            $chromeArgs = @(
                "--user-data-dir=$profile",
                "--remote-debugging-port=$CdpPort",
                "--remote-allow-origins=http://localhost",
                "--disable-session-crashed-bubble",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                "https://arena.ai/"
            )
            Start-Process -FilePath $chrome -ArgumentList $chromeArgs | Out-Null
        }
        $deadline = (Get-Date).AddSeconds(30)
        $cdpReady = $false
        do {
            Start-Sleep -Seconds 2
            try { $targets = @(Get-Json "http://127.0.0.1:$CdpPort/json/list"); $cdpReady = @($targets | Where-Object { $_.type -eq "page" -and $_.url -like "https://arena.ai*" }).Count -gt 0 } catch { $cdpReady = $false }
        } while (-not $cdpReady -and (Get-Date) -lt $deadline)
        if ($cdpReady) { Write-Host "Arena Desktop Chrome/CDP ready." -ForegroundColor Green; try { & $python (Join-Path $RepoRoot "windows\seed-arena-cookies.py") | Write-Host } catch { Write-Warning "Arena cookie seed skipped: $($_.Exception.GetType().Name)" } } else { Write-Warning "Chrome Desktop CDP did not become ready; gateway is still running." }
    } else { Write-Warning "Google Chrome was not found; gateway is running without Desktop browser transport." }
}
Write-Host "Arena gateway ready at $healthUrl" -ForegroundColor Green
Write-Host "Broker status: $brokerUrl" -ForegroundColor DarkGray
