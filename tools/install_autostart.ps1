# Makes Windows start the bot by itself (docs/PRODUCTION_PLAN.md Phase 2): a scheduled task "copybot" that runs
# tools\run_bot.ps1 hidden, and starts it again if it ever stops.
#
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1
#       starts when you sign in to Windows (after a reboot: once you sign in)
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -AtBoot
#       starts when Windows starts, even before anyone signs in (needs PowerShell "Run as administrator")
#   powershell -ExecutionPolicy Bypass -File tools\install_autostart.ps1 -Remove
#       removes the task
param([switch]$AtBoot, [switch]$Remove)

$name = "copybot"
if ($Remove) {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false
    Write-Host "Task '$name' removed."
    return
}

$repo = Split-Path -Parent $PSScriptRoot
$script = Join-Path $PSScriptRoot "run_bot.ps1"
$user = "$env:USERDOMAIN\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $repo `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
if ($AtBoot) {
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # S4U: runs as you without storing your password (it cannot open network drives, which the bot does not need)
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited
} else {
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
    $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
}
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Copybot (Invo copies, paper + live): tools\run_bot.ps1 in $repo" -Force | Out-Null
Write-Host "Task '$name' installed ($(if ($AtBoot) { 'at Windows start' } else { 'at sign-in' }))."
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $name"
Write-Host "The bot writes to logs\copybot.log and logs\console.log in $repo"
