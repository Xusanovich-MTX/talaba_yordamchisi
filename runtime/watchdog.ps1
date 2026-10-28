$ErrorActionPreference = "Continue"
$ProjectDir = "C:\Dgango\talaba_bot"
$PythonExe = "C:\Users\Ma'murov Tohir\.virtualenvs\2-loyiha-LPnabA3I\Scripts\python.exe"
$LogDir = Join-Path $ProjectDir "runtime"

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
}

Set-Location $ProjectDir

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path (Join-Path $LogDir "watchdog.log") -Value "[$timestamp] Starting Talaba Bot..."
    & $PythonExe (Join-Path $ProjectDir "main.py") 1>> (Join-Path $LogDir "bot.stdout.log") 2>> (Join-Path $LogDir "bot.stderr.log")
    $exitCode = $LASTEXITCODE
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path (Join-Path $LogDir "watchdog.log") -Value "[$timestamp] Bot exited with code $exitCode. Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}
