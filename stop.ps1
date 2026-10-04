$projectRoot = $PSScriptRoot
$pidFile = Join-Path $projectRoot 'bot.pid'
if (!(Test-Path -LiteralPath $pidFile)) { Write-Output 'No recorded bot process.'; exit }
$recordedId = [int](Get-Content -LiteralPath $pidFile -Raw)
$processInfo = Get-CimInstance Win32_Process -Filter "ProcessId=$recordedId"
$scriptPath = Join-Path $projectRoot 'bot.py'
if ($processInfo -and $processInfo.Name -match '^python(w)?\.exe$' -and $processInfo.CommandLine.Contains($scriptPath)) {
    Stop-Process -Id $recordedId
    Write-Output 'Shrek Recall stopped.'
} else { Write-Output 'No matching project bot is running; nothing stopped.' }
