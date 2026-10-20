# Registers two scheduled tasks that start Harvey and its dashboard when
# Windows boots, whether or not anyone is logged in, and keep them running
# across crashes and reboots (the .bat files restart them on exit).
#
# Run from an elevated PowerShell (right-click > Run as administrator), as
# the Windows user that is logged in to Claude Code:
#   powershell -ExecutionPolicy Bypass -File deploy\windows\install-tasks.ps1
#
# It asks for that user's Windows password once: Windows needs it to run a
# task while nobody is logged in. The password goes to Task Scheduler only.

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = (Resolve-Path (Join-Path $here "..\..")).Path
$user = "$env:USERDOMAIN\$env:USERNAME"

if (-not (Test-Path (Join-Path $root ".venv\Scripts\harvey.exe"))) {
    throw "Harvey isn't installed in $root yet. Run the install steps first."
}

$cred = Get-Credential -UserName $user -Message "Windows password for $user (lets the tasks run while you're logged out)"
$password = $cred.GetNetworkCredential().Password

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$trigger = New-ScheduledTaskTrigger -AtStartup

foreach ($task in @(
    @{ Name = "Harvey";           Script = "run-harvey.bat" },
    @{ Name = "Harvey Dashboard"; Script = "run-dashboard.bat" }
)) {
    $action = New-ScheduledTaskAction -Execute "cmd.exe" `
        -Argument "/c `"$(Join-Path $here $task.Script)`"" -WorkingDirectory $root
    Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $trigger `
        -Settings $settings -User $user -Password $password -RunLevel Limited -Force | Out-Null
    Start-ScheduledTask -TaskName $task.Name
    Write-Host "Started scheduled task '$($task.Name)'."
}

Write-Host ""
Write-Host "Done. Harvey now starts with Windows and restarts itself if it stops."
Write-Host "Log:       $root\data\harvey.log"
Write-Host "Dashboard: http://localhost:5555 (from a browser on this server)"
Write-Host "To stop:   Stop-ScheduledTask 'Harvey'  (Start-ScheduledTask to resume)"
