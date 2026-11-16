# Launch script for the "Harvey" and "Harvey Dashboard" scheduled tasks.
# Keeps one process running: if it exits for any reason, waits 5 minutes
# and starts it again, forever. Stop it with scripts\windows\harvey-stop.ps1
# (or Stop-ScheduledTask), which ends this loop and the process together.
param(
    [ValidateSet('run', 'dashboard')]
    [string]$Mode = 'run',
    [int]$RestartSeconds = 300
)

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$env:HARVEY_LOG_DAYS = '7'
# The Claude CLI lives in the user's .local\bin; a task's PATH may not have it.
$env:Path = "$env:USERPROFILE\.local\bin;$env:Path"

$exe = Join-Path $Root '.venv\Scripts\harvey.exe'
if ($Mode -eq 'run') {
    $harveyArgs = @('run')
    $env:HARVEY_LOG_FILE = Join-Path $Root 'data\harvey.log'
} else {
    $harveyArgs = @('dashboard', '--host', '127.0.0.1', '--port', '5555')
    $env:HARVEY_LOG_FILE = Join-Path $Root 'data\dashboard.log'
}
$supervisorLog = Join-Path $Root "data\task-$Mode.log"
$crashLog = Join-Path $Root "data\task-$Mode-stderr.log"

function Note([string]$msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Add-Content -Path $supervisorLog -Value $line -Encoding utf8
    # Keep the supervisor log short: last 500 lines.
    $all = Get-Content $supervisorLog -Encoding utf8
    if ($all.Count -gt 500) { $all[-500..-1] | Set-Content $supervisorLog -Encoding utf8 }
}

New-Item -ItemType Directory -Force (Join-Path $Root 'data') | Out-Null
while ($true) {
    Note "starting: harvey $($harveyArgs -join ' ')"
    $p = Start-Process -FilePath $exe -ArgumentList $harveyArgs -WorkingDirectory $Root `
        -NoNewWindow -PassThru -RedirectStandardError $crashLog
    $null = $p.Handle   # keep a handle, or ExitCode comes back empty
    Note "pid $($p.Id)"
    $p.WaitForExit()
    Note "exited with code $($p.ExitCode); restarting in $RestartSeconds s (stderr: $crashLog)"
    Start-Sleep -Seconds $RestartSeconds
}
