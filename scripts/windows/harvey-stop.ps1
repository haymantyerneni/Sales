# Stops Harvey completely: ends both scheduled tasks and any harvey.exe
# from this project. Add -Disable to keep them off after a reboot too
# (re-enable with Enable-ScheduledTask "Harvey", "Harvey Dashboard").
param([switch]$Disable)

$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
foreach ($name in 'Harvey', 'Harvey Dashboard') {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($Disable) { Disable-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue | Out-Null }
}
# The launch-script loop and the Python processes, in case any outlived the task.
Get-CimInstance Win32_Process | Where-Object {
    ($_.CommandLine -like '*harvey-task.ps1*') -or
    ($_.ExecutablePath -like "$Root\.venv\*") -or
    ($_.CommandLine -like "*$Root\.venv\Scripts\harvey.exe*")
} | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item (Join-Path $Root 'data\harvey.pid') -ErrorAction SilentlyContinue
Write-Output ("Harvey stopped{0}." -f $(if ($Disable) { ' and disabled until you re-enable it' } else { '' }))
