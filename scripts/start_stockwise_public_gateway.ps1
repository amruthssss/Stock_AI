Set-Location $PSScriptRoot\..
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
$pidFile = Join-Path (Get-Location) 'data\stockwise-gateway.pid'
$port = 8010

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python environment not found: $python"
}
if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $port is already occupied."
}
try {
    $backend = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/api/v1/health/ready' -TimeoutSec 5
    if ($backend.StatusCode -ne 200) { throw 'Backend readiness check failed.' }
} catch {
    throw 'Backend on 127.0.0.1:8000 is not reachable.'
}

$process = Start-Process -FilePath $python -ArgumentList @(
    '-m', 'backend.gateway', '--host', '127.0.0.1', '--port', $port,
    '--upstream', 'http://127.0.0.1:8000', '--pid-file', $pidFile
) -WorkingDirectory (Get-Location) -PassThru -WindowStyle Hidden

$listening = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 250
    if ($process.HasExited) {
        throw 'Gateway failed to start.'
    }
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        $listening = $true
        break
    }
}
if (-not $listening) {
    Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    throw 'Gateway did not begin listening before the startup timeout.'
}
Write-Output "StockWise local gateway started on 127.0.0.1:$port (PID $($process.Id))."
