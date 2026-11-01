Set-Location $PSScriptRoot\..
$pidFile = Join-Path (Get-Location) 'data\stockwise-gateway.pid'
if (-not (Test-Path -LiteralPath $pidFile)) {
    Write-Output 'StockWise local gateway is not running.'
    exit 0
}
$processId = [int](Get-Content -LiteralPath $pidFile -Raw)
$process = Get-Process -Id $processId -ErrorAction SilentlyContinue
if ($process) {
    Stop-Process -Id $processId
    Write-Output "Stopped StockWise local gateway process $processId."
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
