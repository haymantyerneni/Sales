# Registers the "Harvey" and "Harvey Dashboard" scheduled tasks.
#   - start at system startup, run whether or not anyone is logged on
#     (S4U: no stored password; local files and internet work, Windows
#     network shares don't, and Harvey doesn't use any)
#   - highest privileges, no execution time limit
#   - Task Scheduler restarts a failed task every 5 minutes (999 tries, the
#     maximum it allows); the launch script's own loop restarts the process
#     every 5 minutes with no limit, which covers the rest
# Run from an elevated PowerShell. -Disabled registers them switched off.
param([switch]$Disabled)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$launcher = Join-Path $Root 'scripts\windows\harvey-task.ps1'
$user = "$env:COMPUTERNAME\$env:USERNAME"

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Highest
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -RestartInterval (New-TimeSpan -Minutes 5) -RestartCount 999 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -DontStopOnIdleEnd `
    -StartWhenAvailable -MultipleInstances IgnoreNew

foreach ($t in @(@{Name = 'Harvey'; Mode = 'run'; Desc = 'Harvey heartbeat (harvey run)'},
                 @{Name = 'Harvey Dashboard'; Mode = 'dashboard'; Desc = 'Harvey dashboard on 127.0.0.1:5555'})) {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' -WorkingDirectory $Root `
        -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$launcher`" -Mode $($t.Mode)"
    Register-ScheduledTask -TaskName $t.Name -Description $t.Desc -Action $action `
        -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    if ($Disabled) { Disable-ScheduledTask -TaskName $t.Name | Out-Null }
    Write-Output ("registered: {0} ({1})" -f $t.Name, $(if ($Disabled) { 'disabled' } else { 'enabled' }))
}
