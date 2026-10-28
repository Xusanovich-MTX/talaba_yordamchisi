@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -in @('python.exe','pythonw.exe') -and $_.CommandLine -like '*talaba_bot*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Bot to'xtatildi.
