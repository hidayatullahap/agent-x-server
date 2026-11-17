# Windows: start agent.py when you log in and restart it if it stops.
#
#   powershell -ExecutionPolicy Bypass -File service\windows-task.ps1
#
# Remove it with: Unregister-ScheduledTask -TaskName "kucing-ag agent"
# The agent reads kucing-ag.json from this repository's folder, then
# %USERPROFILE%\.kucing-ag\config.json. Its log goes to agent.log in the same folder.

$folder = Split-Path $PSScriptRoot
$python = (Get-Command python).Source
$run = "`"$python`" agent.py >> agent.log 2>&1"

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c $run" -WorkingDirectory $folder
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)

Register-ScheduledTask -TaskName "kucing-ag agent" -Action $action -Trigger $trigger -Settings $settings -Force
Start-ScheduledTask -TaskName "kucing-ag agent"
