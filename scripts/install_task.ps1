# Registers a Task Scheduler task that starts the bot when you log on.
# Run once in PowerShell as your own user. Re-running replaces the task.
$script = Join-Path $PSScriptRoot "run_bot.ps1"
$root = Split-Path -Parent $PSScriptRoot
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $root `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "Evil Donations" -Action $action -Trigger $trigger `
    -Settings $settings -Description "Evil Donations Discord bot" -Force
