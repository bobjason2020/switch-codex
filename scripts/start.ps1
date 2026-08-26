param(
    [string]$ListenHost = $(if ($env:SW_HOST) { $env:SW_HOST } else { "127.0.0.1" }),
    [int]$ListenPort = $(if ($env:SW_PORT) { [int]$env:SW_PORT } elseif ($env:SR_PORT) { [int]$env:SR_PORT } else { 4100 })
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Root ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Logs = Join-Path $Root "logs"
$OutputLog = Join-Path $Logs "switchyard.windows.log"
$ErrorLog = Join-Path $Logs "switchyard.windows.error.log"
$HealthUrl = "http://127.0.0.1:${ListenPort}/health"

New-Item -ItemType Directory -Force -Path $Logs, (Join-Path $Root "data") | Out-Null

if (-not (Test-Path $Python)) {
    $PythonLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($PythonLauncher) {
        & $PythonLauncher.Source -3 -m venv $Venv
    } else {
        $PythonLauncher = Get-Command python -ErrorAction SilentlyContinue
        if (-not $PythonLauncher) {
            throw "Python 3 was not found. Install it from https://www.python.org/downloads/windows/ and run this command again."
        }
        & $PythonLauncher.Source -m venv $Venv
    }
    if ($LASTEXITCODE -ne 0) { throw "Unable to create the Python virtual environment." }

    Write-Output "installing deps..."
    & $Python -m pip install -q -r (Join-Path $Root "requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Unable to install Python dependencies." }
}

try {
    $Response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
    if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300) {
        Write-Output "already healthy on :$ListenPort"
        Write-Output $Response.Content
        exit 0
    }
} catch {
    # No healthy service is listening; start a new background process below.
}

$Process = Start-Process -FilePath $Python `
    -ArgumentList @("-m", "uvicorn", "app:app", "--host", $ListenHost, "--port", "$ListenPort") `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $OutputLog `
    -RedirectStandardError $ErrorLog `
    -WindowStyle Hidden `
    -PassThru

for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {
    Start-Sleep -Milliseconds 300
    try {
        $Response = Invoke-WebRequest -UseBasicParsing -Uri $HealthUrl -TimeoutSec 2
        if ($Response.StatusCode -ge 200 -and $Response.StatusCode -lt 300) {
            Write-Output "ready  http://${ListenHost}:${ListenPort}/  (pid: $($Process.Id))"
            Write-Output $Response.Content
            exit 0
        }
    } catch {
        # Keep polling while uvicorn initializes.
    }
}

Write-Error "failed to start; see $OutputLog and $ErrorLog"
Get-Content -Path $OutputLog, $ErrorLog -Tail 40 -ErrorAction SilentlyContinue
exit 1
